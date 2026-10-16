# fix(rag): follow-up rewrite reaches every retrieval seam, and eval/live seam parity is restored (#713)

## What changed

`RAGEngine.query()` documented that a referential follow-up ("tell me more")
is retrieved against a self-contained rewrite of the question, but only the
dense-embedding seam consumed the rewrite; the BM25/FTS `query_text`, the
cross-encoder rerank `query`, both CRAG evaluators, the context distiller,
and the multi-sub-query orchestration all still ran on the raw follow-up.
Adjacent defects in the same hot path compounded this: the eval surface
measured a pipeline the live chat path does not run, blank non-stream
completions were persisted as successful answers, the decomposition and eval
embed gathers (plus the orchestration fallback embed) ran outside the shared
EMBEDDING admission budget, and an admission-rejected turn leaked overlap
tasks plus un-awaited embed coroutines (audit findings T1-32-K-10
HIGH, T1-32-K-02, T1-32-K-11, T1-32-S2-02, T1-32-S2-03).

- **The rewrite now reaches every retrieval seam.** `query()` passes
  `retrieval_query` (the rewrite when one fired, the raw input otherwise) to
  the standard-route `_execute_retrieval` call — which threads it into the
  FTS `query_text`, the rerank `query`, and the CRAG evaluator — to the
  multi-sub-fusion evaluator, to the context distiller, and to the
  decomposition orchestration (whose fallback also stops re-embedding the
  raw follow-up). Memory search, memory intent detection, the follow-up
  detector itself, and answer generation (`build_messages`) keep the raw
  user input by design. Non-follow-up queries take identical inputs at
  every changed seam (`retrieval_query == user_input` when no rewrite
  fired); the blank-completion and admission changes below apply to all
  queries.
- **Eval/live seam parity (seam level).** `_retrieve_eval_outcome` now
  unpacks `_execute_retrieval`'s `rerank_success` and threads
  `reranked=rerank_success if rerank_success is not None else False` into
  `filter_relevant`, mirroring `_retrieve_document_sources_for_kinds`, so
  `retrieve_eval_results` / `query_retrieve_only` no longer re-apply the raw
  distance threshold to results the live path keeps after a successful
  rerank. Parity level: SEAM parity, per the issue's recorded default (and
  absent a maintainer decision to the contrary); output parity (applying
  chat's post-filter feedback reranking and distillation inside eval) is
  deliberately out of scope. Two operator-visible consequences: a non-stream
  completion that is blank on every configured client now fails with HTTP
  503 (previously it returned a blank 200 — the defect), and retrieval-eval
  metrics can shift where reranking succeeded, so benchmark runs from
  before and after this change are not directly comparable.
- **Blank non-stream completions fall through.** `_get_llm_response` now
  applies the same visible-answer contract as its streaming sibling
  (classified at the content level; the streaming sibling tracks emitted
  chunks): an
  empty or reasoning-only completion sets `last_error` and tries the next
  fallback client; if no client produces visible content the call raises
  `RAGEngineError` (the non-stream equivalent of the streaming LLM_ERROR
  chunk) instead of persisting a blank 200 answer.
- **Embeds share the budget.** The decomposition sub-query gather, the
  orchestration fallback embed, and `_build_query_embeddings` (the
  eval/`retrieve_sources` query embed) now run inside
  `admit(AdmissionClass.EMBEDDING)`. A rejection degrades per surface:
  orchestration falls back to single-query retrieval using the
  already-embedded variants; eval reports `status="unavailable"` (the
  existing total-outage contract); `retrieve_sources` records the document
  kind as failed.
- **AdmissionRejected turns clean up.** The variant coroutines are built
  inside the admitted context (a rejected admission never creates them),
  and the rejection handler cancels the memory/KMS overlap tasks first,
  like every sibling early exit.

## Tests

Nine frozen acceptance checks (RED at the pre-fix commit, GREEN at the PR
head) across five new suites — `test_c08_followup_rewrite_seams.py`,
`test_c08_eval_seam_parity.py`, `test_c08_nonstream_blank_fallback.py`,
`test_c08_embedding_admission.py`, `test_c08_admission_rejected_cleanup.py`
— plus additive `test_c08_residual_seams.py` (degradation contracts,
fusion-evaluator/distiller seams, blank-completion raise paths, admission
placement/rejection exits) and a structural AST guardrail
(`test_c08_seam_census.py`) pinning the wiring: every query()-scoped seam
passes `retrieval_query` (positional or keyword form — unrecognized call
shapes fail loud), no `filter_relevant` call site hardcodes `reranked=False`
or omits the status (the documented `_filter_relevant` shim excepted), every
`embed_tasks` construction (whatever the shape, with a site-count floor)
sits inside the EMBEDDING admission, and the nested `_fallback_embedding`
provider embed is scanned by its enclosing function. The census ships
self-tests that feed known-bad mutated sources through the same matchers and
assert they are flagged, so it cannot silently fail open.
One collateral fixture in `test_rag_engine.py`
(`test_query_require_vault_allows_with_vault_id`) relied on the old
blank-as-success behavior and was realigned to a non-empty fake response —
the test's own contract (only the vault guard must not fire) is unchanged.

## Known limitations

- Parity is seam-level by the issue's recorded default; eval does not apply
  chat's post-`filter_relevant` feedback reranking or context distillation
  (output parity is a maintainer decision that remains open).
- A non-stream completion that is blank on every configured client now fails
  with HTTP 503 (the pre-existing `RAGEngineError` mapping) instead of the
  defective blank 200; streaming surfaces the same condition in-band as an
  `LLM_ERROR` event. Retrieval-eval metrics can shift where reranking
  succeeded, so benchmark runs from before and after this change are not
  directly comparable.
- The opt-in agentic RAG route (`agentic_rag_enabled`, off by default)
  returns before the follow-up rewrite is computed and still retrieves with
  the raw input; its seams are owned by #715 and the admission follow-up.
- Provider embed calls outside the EMBEDDING admission by self-declared
  triage (none are in the issue's enumerated sites for this issue): the
  memory pipeline's `embed_query` (rag_engine.py), the context distiller's
  internal dedup `embed_batch`, and the agentic/search/eval/memories/
  image-search provider embeds (agentic_tools.py, search.py, eval.py,
  memories.py, image_search.py). Bringing these inside the budget is tracked
  in #959.
- The vision late-fusion service (issue #462) still conditions on the raw
  user question — a generation-side input by design, not a retrieval seam.
