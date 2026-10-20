# Query-transformer hardening: prompt boundaries, linear filename heuristic, message cap, cache integrity, rewrite_followup contract (#918, #922, #924, #928)

Collated follow-up PR closing the four open issues filed from the #711 /
#919 review rounds. All changes are backend; no schema migration, no
frontend change. One new setting (`CHAT_MESSAGE_MAX_CHARS`, default
20000) and one cache-key version bump (`PROMPT_VERSION` v1 → v2, one-time
invalidation of existing query-transform/plan cache entries at deploy).

## What changed

- **#918 — SECURITY BOUNDARY directives on the step-back and HyDE
  prompts.** Both LLM-facing system prompts in `query_transformer.py`
  now carry the same untrusted-data directive #711 shipped for
  `rewrite_followup`, naming exactly the tags each prompt emits
  (`<user_query>`). The corrupt-Redis-payload hole is closed: a
  parsed-but-empty or wrong-shape step-back cache entry (e.g. the raw
  `"[]"`) is discarded with a best-effort `delete` (never a `setex`, so
  the #711 hit-no-TTL-refresh pin holds) and answered with the
  `[('original', query)]` arm instead of an empty variants list. With
  `hyde_enabled=True` a corrupt hit previously returned a list with NO
  original variant at all; it now returns the original-only arm.
- **#922 — the filename heuristic is worst-case linear and live messages
  are capped.** `_is_exact_or_document_query`'s filename regex was
  quadratic on hyphen-joined runs (~160 s synchronous on the event loop
  for a 200k-char message; reviewer/critic measured 7.2 s @ 40k, 214 s @
  200k). It is replaced by an extension-anchored scan (literal `.<ext>`
  finditer + backward `[\w\-]`-run walk accepting iff the run contains a
  word character) that is provably equivalent and linear. Both chat
  entry routes now reject over-cap LIVE messages with a 422 before any
  generation work: `POST /api/chat` `message` and the FINAL message of
  `POST /api/chat/stream` (`CHAT_MESSAGE_MAX_CHARS`, default 20000;
  `{"messages": []}` keeps the route's own 400). Prior-turn history is
  deliberately NOT bounded — legitimate assistant answers can be long;
  a pathological paste now surfaces an error instead of blocking every
  user's requests for minutes.
- **#924 — planner copy discipline, one shared cache-write helper, and
  the three accepted-cost decisions recorded in code.** `QueryPlanner.
  plan` returns copies on both the cache-hit and fresh paths, so caller
  mutation can no longer poison the LRU (#510 QUERY-001 discipline); the
  planner Redis hit path also validates payload shape before serving
  (sibling of the #918 guard). The three hand-rolled cache-write sites
  (step-back, HyDE, planner) route through one shared
  `_cache_write_success` helper owning the module's only `setex`
  reference, so the write contract cannot diverge again. The
  DECISION(no-negative-cache), DECISION(shared-breaker), and
  DECISION(content-addressed-keys) markers record the accepted costs
  with their rationale at the sites that accept them.
- **#928 — rewrite_followup handles degenerate prior-turn shapes by its
  documented contract.** An empty/whitespace newest assistant turn no
  longer shadows older non-empty assistant context (the scan skips it
  and keeps looking), and a whitespace-only LLM response returns `None`
  (the documented failure signal) instead of raising IndexError out of
  the method.

## Upgrade notes

- Set `CHAT_MESSAGE_MAX_CHARS` to change the live-message cap (minimum
  1). Requests exceeding it get a 422 before any generation work.
- Existing Redis query-transform/query-plan entries (v1 keys) miss once
  after deploy and repopulate on demand; no operator action required.
