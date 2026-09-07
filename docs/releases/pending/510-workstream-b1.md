# Release: Workstream B1 — retrieval identity, evidence preservation, answer controls (#510)

## Summary

Repairs the full Workstream B1 finding set from issue #510 (RAG-001…008,
QUERY-001, CHAT-003, CITE-001/002, VECTOR-004, UI-004, PRODUCT-ENH-04, plus
legacy #258 ENH-014 and #229 legacy-05) and publishes the source/score/
citation wire schema (docs/engineering/source-score-citation-schema.md).

## Operator-visible changes

- Chat temperature, retrieval mode (auto/semantic/keyword), citation mode
  (enabled/disabled/required) and a typed metadata filter (date range, tags,
  author) are now honored end to end. Previously temperature and both mode
  selectors were accepted but ignored; filters did not exist. Unknown mode
  values and unknown filter fields are rejected with 422 instead of being
  silently ignored.
- `citation_mode: "required"` adds an explicit `citation_enforcement` status
  to the done payload (`satisfied` / `missing_citations`) — an uncited answer
  is visibly flagged, never silently returned.
- Supersession currency warnings now reach the answer payload
  (`currency_warnings`) and the chat UI, in addition to the existing prompt
  hint.
- Answers now carry `answer_contract.abstention_basis`
  (`decision`/`unavailable`); the abstention flag is an explicit pipeline
  decision rather than a substring guess, so a sourced answer quoting
  "don't know" is no longer marked abstained.
- Agentic answers: rerank status is honored (reranked evidence keeps reranker
  scores instead of being re-filtered by distance), and source labels are
  globally unique across retrieval rounds, matching synthesis citations and
  cards.
- Retrieval correctness: window expansion no longer duplicates current-format
  (reupload-safe) chunks and every serialized source id resolves through the
  exact-ID preview lookup; short unique evidence (dates, codes) survives
  distillation; wiki overlap removes only the covered sentences, keeping
  unique facts; flat vector scans apply the configured metric (cosine/L2)
  explicitly; wiki FTS threshold 0 now means "always run" as documented;
  the query-transformation cache composes HyDE per current feature flags on
  every request (identical cold/warm behavior, Redis and LRU); a transient
  schema-probe failure no longer permanently disables supersession warnings.
- Citation cleanup never modifies fenced/inline code bytes, and citation
  validity is the actual label set (phantom labels in sparse numberings are
  invalid).

## Compatibility / rollout / rollback

- BEHAVIOR CHANGE: `retrieval_mode` / `citation_mode` on the chat endpoints
  were previously free strings accepted and ignored; they are now
  Literal-validated. Callers sending unsupported values (e.g. `"hybrid"`)
  will now receive 422 instead of silent no-op. All values the UI ever
  offered remain valid.

- All API additions are optional request fields and additive response
  fields; previously stored messages deserialize unchanged (guard tests
  cover legacy payloads and feature-off defaults).
- One additive, idempotent migration (auto-applied by `run_migrations` on
  startup) adds two nullable `chat_messages` columns — `currency_warnings`
  and `citation_enforcement`. No new environment variables, no dependency
  changes.
- Keyword/semantic retrieval modes change results only for callers that
  explicitly select them; default (`auto`/absent) ranking is unchanged and
  covered by control tests.
- Rollback: revert the PR. The two nullable columns are inert without the
  code that reads them, so no data migration is needed to undo — they can
  be left in place or dropped manually.

## Evidence

Frozen acceptance checks C1–C19 (base `a6b96af` RED → fixed tree GREEN, 17
discriminating; 2 preserving GREEN→GREEN) plus the backend/frontend
regression suites named in the PR. Trace:
`.agents/issue-traces/510-repair-retrieval-identity-evidence-answer-controls/`
(unpublished working artifacts).
