# Embedding provider error bodies redacted from default-level logs (C15, Issue #903)

## What changed

- `backend/app/services/embeddings.py` adds the module-level helper
  `_provider_body_summary()`: it returns a bounded, non-echoing summary of a
  provider HTTP error body (`body_length=<n> body_sha256=<12 hex chars>`) and
  emits the full body at DEBUG only. Length+hash was chosen over a
  first-N-chars preview because any body excerpt can carry echoed user text.
- All nine `logger.warning` f-strings that previously interpolated the raw
  provider error body (`response.text`) now interpolate
  `_provider_body_summary(response.text)` instead:
  - single-text embed (`embed_single`): outage-status warning inside the
    breaker wrap, non-200 warning outside it, and the invalid-JSON warning;
  - health probe (`embed_probe`): the same three paths;
  - batch embed (`_embed_batch_with_retry`): the outage-status warning, the
    token-overflow warning, and the generic non-200 warning.
- The two non-logging `response.text.lower()` token-overflow sniffs are
  unchanged (they never reach a log record). Outage classification
  (`_is_outage_status`), retry/circuit-breaker behavior, and the raised
  `EmbeddingError` messages (which never carried the body) are unchanged.
- New regression tests in
  `backend/tests/test_c15_embedding_error_body_redaction.py` pin all three
  acceptance criteria: a marker-bearing provider body (`C15ET_MARKER`) never
  reaches a WARNING-or-higher record on any of the nine paths (AC1);
  failures still log WARNING with status code, provider mode, and the
  bounded summary, and the raised error keeps the status code (AC2); and
  the token-overflow path leaks neither the body nor an echoed input
  sentinel while split/mean-pool recovery still completes (AC3).

## Why

Provider error bodies are third-party responses that can echo the request
payload back — token-overflow errors in particular quote the offending
input text — and embedding inputs are user-derived document/chunk/query
content, so the raw body violates the C15 disclosure contract (#287: no raw
user-derived text at default log level) on this surface. #710 closed the
app-side query-text sites but deliberately excluded the generic `.text`
attribute from its AST guard; #903's Phase 4.2 recurrence census identified
these nine WARNING sites as the residual surface. Operational signal is
preserved: status codes, modes, and bounded summaries still flow at WARNING,
and the full body stays reachable at DEBUG.

## Migration steps

None. No configuration, API, schema, or client-facing behavior changes.

## Known caveats

- The full provider body is still emitted at DEBUG; deployments running
  DEBUG-level logging will see it. That is intended and outside the #287
  default-level contract.
- #710's AST guard (`backend/tests/test_no_query_text_in_default_logs.py`)
  still deliberately does not cover the generic `.text` attribute (too
  generic to attribute safely across modules); the new dedicated test file
  pins the embedding surface instead.
