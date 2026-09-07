# Source / Score / Citation Wire Schema

Authoritative contract for the evidence metadata serialized by the chat
pipeline (stream SSE done payload and non-stream response). Consumed by the
frontend chat components and by downstream workstreams (roadmap #510 B1;
consumers A1/A2/F1). All additions described here are additive and optional —
previously stored messages deserialize unchanged.

## Source identity (`sources[].id`)

The `id` on every serialized source card is the **actual LanceDB record id**,
resolved through the exact-ID preview lookup (`GET /search/chunks/{id}/context`
→ `get_chunks_by_uid`, exact match only):

- Current format (reupload-safe, default since `reupload_safe_order=True`):
  `{file_id}_{hash8}_{scale}_{index}` e.g. `42_ab12cd34_default_0`.
- Legacy format: `{file_id}_{scale}_{index}` or `{file_id}_{index}`.

Rules:

- A legacy id is **never reconstructed** for a content-hash record. If a
  record stores a hashed id, that exact id is what reaches the chat payload
  and what the preview endpoint must resolve.
- Window expansion preserves identity: the center passage appears exactly
  once; the expanded (adjacent) sources carry their own stored record ids.
- Two distinct artifacts with identical text remain distinct sources
  (identity is file-scoped / artifact-scoped, not text-scoped).

## Source labels (`source_label`)

`S1, S2, …` are **stable positional labels** assigned over the serialized
source list. Wiki evidence uses `W#`, KMS `K#`, memories `M#`.

- Agentic retrieval labels extend **one global sequence** across rounds: round
  2's first source is `S{n+1}`, matching the synthesis prompt, inline
  citations, and source cards (no duplicate `S1`).

## Scores (`score`, `score_type`)

`score_type` tells the consumer how to interpret each source's `score`:

| value | meaning | polarity |
|---|---|---|
| `distance` | vector distance (cosine or L2 per `vector_metric`) | lower = better |
| `rerank` | reranker relevance score | higher = better |
| `rrf` | reciprocal-rank fusion score (hybrid) | higher = better |

`vector_metric` (default `cosine`) is applied explicitly on **every** dense
query — flat scans as well as ANN — so distance semantics agree with the ANN
index configuration.

## Answer contract (`answer_contract`)

```json
{
  "answer": "…",
  "citations": [{"label": "S1", "evidence_type": "document|memory|wiki|kms|image|chart|table|equation|code"}],
  "abstained": false,
  "abstention_basis": "decision" | "unavailable"
}
```

`abstained` reflects an **explicit decision** (`abstention_basis =
"decision"`): the pipeline decided the answer abstained (no citations and no
citable evidence) or answered from evidence (any citation). When no decision
is available (legacy prose), `abstention_basis = "unavailable"` and
`abstained` carries no meaning — it is never guessed from the answer text
(a factual answer quoting "don't know" is not abstaining).

## Honesty fields (PRODUCT-ENH-04)

Retrieval relevance (`score`/`score_type`), textual support
(`citation_confidence`, `unverifiable_claims`), and source freshness
(`currency_warnings`) are separate, never conflated:

- `currency_warnings: string[]` — supersession/currency advisories (a
  retrieved document may have a newer version in the vault). Surfaced in the
  done payload on **both** stream and non-stream paths.
- Lexical overlap scores are support measurements, not probabilities of
  correctness.

## Citation enforcement (`citation_enforcement`)

Present when the request's `citation_mode` is `"required"`:

```json
{"mode": "required", "status": "satisfied" | "missing_citations", "detail": "…"}
```

`missing_citations` means the answer contained no valid citation labels while
citable sources existed — the response is flagged, never silently returned as
an ordinary uncited answer.

## Request controls

On `POST /chat` and `POST /chat/stream` (all optional; defaults preserve
previous behavior):

- `temperature: number|null` — forwarded verbatim to the provider for both
  streaming and non-streaming generation; omitted keeps the provider default.
- `retrieval_mode: "auto" | "semantic" | "keyword"` — `semantic` = dense-only
  retrieval; `keyword` = pure lexical (BM25) retrieval; `auto` = configured
  hybrid. Unknown values are rejected (422). NOTE: these fields previously
  accepted arbitrary strings and ignored them; they are now Literal-validated,
  so a caller passing an unsupported value (e.g. `"hybrid"`) gets a 422
  instead of silent no-op behavior.
- `citation_mode: "enabled" | "disabled" | "required"` — `disabled` removes
  the citation instruction from the prompt; `required` strengthens it and
  enables the `citation_enforcement` field. Unknown values are rejected (422)
  (same Literal tightening as above). Both controls apply on the standard
  pipeline AND the agentic path (`agentic_rag_enabled=True`): the agentic
  synthesis prompt honors `citation_mode`, and the agentic done payload
  carries `citation_enforcement` / `currency_warnings` with the same
  semantics.
- `metadata_filter` — typed, documented subset; **unknown fields are
  rejected (422), never silently ignored**:

```json
{
  "date_from": "2026-01-01",   // files.document_date >= date_from
  "date_to": "2026-02-01",     // files.document_date <= date_to
  "tags": ["ops", "launch"],   // vault-scoped document tag assignment (document_tags × tags)
  "author": "alice@example.com" // files.email_sender (recorded for email-sourced documents)
}
```

Semantics:

- Resolution is **vault-scoped**; the resolved file-id set never crosses
  vault boundaries.
- Files with no recorded `document_date` are excluded while a date filter is
  active (an unknown date cannot satisfy a range).
- `author` matches the persisted sender of email-sourced documents — the only
  author-like field the vault records today; documents without one simply do
  not match.
- **Zero matches are real results**: when no file satisfies the filter (or
  the metadata tables are unavailable), retrieval applies a zero-match filter
  and returns no sources — a filter is never silently dropped.
- The filter applies with parity across retrieval paths: single, fused
  multi-sub-query, and agentic retrieval.
- Retrieval-only mode (`retrieval_mode` keyword with an empty query text)
  degrades gracefully to dense retrieval and logs at debug; chat always
  supplies the user's message as the lexical query.

## Versioning

This schema evolves additively: new fields are optional, old stored messages
deserialize unchanged, and feature-off defaults reproduce the previous
ranking and response shapes exactly.
