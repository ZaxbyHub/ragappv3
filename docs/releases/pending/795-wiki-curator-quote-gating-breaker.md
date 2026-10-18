# fix(wiki): LLM curator verifies quotes against the aligned window, gates verified status per candidate, and shares one breaker per endpoint (#795)

## What changed

Five independent defects in the optional LLM wiki curator pipeline
(`wiki_curator.py`, `wiki_curator_client.py`, `wiki_compiler.py`, `config.py`)
are fixed. The curator is default-off (`wiki_llm_curator_enabled=False`), so
none of these affect a default deployment; all of them degraded correctness
or reliability when the curator is enabled.

- **Quote verification now compares numbers against the ALIGNED WINDOW, not
  the whole chunk.** The fuzzy path's numeric-identity gate demanded the
  quote's digit multiset equal the ENTIRE chunk's, so any chunk carrying one
  extra number (page header, date, step index) failed even a 96-scoring
  match. `fuzz.partial_ratio_alignment` now supplies the matched span
  (`dest_start:dest_end`), and the quote's numbers must be CONTAINED IN
  (multiplicity-sensitive) that window's numbers. A quote that mutates or
  invents a number its window lacks is still rejected — including the
  subtle case where the mutated value occurs elsewhere in the chunk outside
  the window (pinned by test). `verify_quote` (the PUT /wiki/claims
  active-transition re-verification, which passes the whole file text)
  inherits the fix. Whitespace-only quotes/sources no longer verify.
- **The prompt schema requests only what the pipeline consumes.** The
  `entities` and `relations` output sections — never read by any consumer —
  are removed, along with the per-claim `page_title`/`page_type` fields
  (zero consumers; also dropped from `CuratorAcceptedClaim`). This stops
  spending the fixed 2048-token output budget on discarded fields and
  removes the largest post-claims truncation window that turned a
  token-capped reply into a wholesale `json_parse_failed` claim loss.
- **`active_if_verified` mode now computes verification per candidate.**
  Previously the mode stamped `active` unconditionally on its branch while
  the adjacent comment claimed the quote check had passed — false whenever
  `require_quote_match` was off. Now a claim is `active` only when its quote
  check ran AND passed AND its chunk_id was one we fed the curator. An
  unknown chunk_id with `require_chunk_id` off is KEPT as `needs_review`
  with its own cited id (never a fabricated `chunks[0]` provenance, which
  used to mint active claims with invented lineage). Draft mode is
  unchanged (`needs_review` always).
- **`wiki_llm_curator_max_input_chars` is documented truthfully.** The knob
  is a per-document curated span (from the start of the text; the compiler
  takes `max(2000, knob)` chars and windows them into 2000-char chunks),
  while each call's prompt separately re-applies the bound to its own chunk
  snippet. The old docstring called it a per-call bound; the config
  docstring, the compiler comment, and `build_user_prompt`'s docstring now
  all state the two scopes consistently. No behavior change.
- **The curator circuit breaker now accumulates across compile jobs.** Every
  job built its own `AsyncCircuitBreaker(fail_max=5)` and could make at most
  ~3 calls, so the breaker could never open against a hard-down endpoint.
  Breakers now live in a module-level registry keyed by (endpoint, model)
  and are shared by every client for that endpoint. Failure classification
  follows the repo's OPS-002 (#494) pattern: only 5xx/429 (and
  connection/timeout errors) count toward `fail_max`; a non-outage 4xx
  (misconfigured model name) raises without tripping the shared breaker.
  Every curator run now dispatches through the single-worker wiki-curator
  offload executor, so concurrent jobs cannot drive one breaker from two
  concurrent event loops.

## Why

Frontier-audit findings T1-35-K-05, TQ-instruction-batch-5-05,
TQ-instruction-batch-5-03 (+ T1-35-K-12), T1-35-K-11, T1-35-S-05 (+
T1-35-S2-05): a 96.7-scoring multi-number quote was rejected because the
numeric gate compared against the whole chunk; a reply truncated inside the
never-read `entities` section discarded every complete claim;
`active_if_verified` activated unverified claims (including fabricated
chunks[0] provenance); a 118,670-char document was curated over only its
first 6000; and a permanently-down curator endpoint was re-probed by every
compile job forever.

## Migration / compatibility

No migration required. No settings keys added, removed, or renamed
(docstring-only config change). `CuratorClient` drops the unused
`breaker_name` constructor parameter (no caller passed it). Claims kept
under the flag-off unknown-chunk tolerance persist with `file_id=None`,
which the store and the claim PUT route already handle. Frontend needs no
change (the settings label carries no per-call phrasing).

## Known limitations / residuals

- A reply truncated inside `open_questions`/`contradictions` is still
  rejected wholesale as `json_parse_failed` (the complete claims array is
  not salvaged); removing `entities`/`relations` shrank, not eliminated,
  that loss window. Pinned by test.
- Numeric containment is order/position-blind: the quote's numbers are
  checked for multiset containment in the aligned window, so a quote whose
  numbers are SWAPPED relative to the source ("region A had 5 ... region B
  had 3" restated as "region A had 3 ... region B had 5") verifies at
  >= 92 whenever the window carries both values. This IS constructable
  end-to-end (shapes score 94-98) and is pinned as documented residual
  behavior by test; a classical LLM paraphrase error can therefore
  auto-activate under `active_if_verified`. Window edges are now expanded
  to whole digit-token boundaries (a truncated prefix like "1215" for a
  source "121563981" no longer verifies), but a quote that is a verbatim
  character substring of the source (e.g. "5000" inside "50000") still
  verifies via the strict substring path -- pre-existing behavior,
  unchanged by this PR. An order-aware numeric check is future work.
- A non-outage >= 300 response records SUCCESS on the shared breaker
  (OPS-002 pattern-consistent), so interleaved config errors reset the
  CLOSED failure counter and can defer `fail_max` accumulation; a 4xx
  seen while HALF_OPEN likewise closes the breaker (success_threshold=1).
  The breaker identity is the constant "curator" (the breaker identity
  never carries the operator URL).
- Cross-job curator calls now serialize through the single-worker offload
  executor (the concurrency knob still bounds parallel calls within one
  job); the executor wait is untimed, so a very slow curator run delays
  subsequent curator runs process-wide. Direct `CuratorClient.propose()`
  callers outside the compiler could drive one breaker from concurrent
  event loops; the nanosecond lock window is disclosed and pinned by a
  concurrency test.
- An accepted claim whose cited chunk was unknown (`require_chunk_id`
  off) persists with a NULL-file source row: it can never be auto-promoted
  via the claim PUT route (which requires a document-file source), it is
  invisible to file-scoped bookkeeping (stale-marking, page staleness),
  and the source dedupe treats all NULL-file document sources of the same
  claim as one lineage (a second distinct quote/chunk for the same claim
  text is not re-attached).
- Claims that pre-date this PR and carry `chunks[0]`-fabricated file
  lineage (minted before the fallback was removed) are not repaired or
  re-attributed by this change.
- Reactivation is now gated on the candidate's own computed status: a
  `needs_review` candidate (draft mode, unknown chunk, quote check off)
  can no longer flip a superseded claim to active; previously such a flip
  also deleted the claim's open `stale` lint finding.
- The per-document span is `max(2000, max_input_chars)` — a knob set below
  2000 still curates 2000 chars (one chunk); pre-existing behavior, now
  stated in the compiler comment.
- `CuratorClient.test_connection` is pre-existing dead code (the settings
  test route builds its own HTTP client); recorded, not removed in this PR.
