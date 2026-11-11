# Prompt-resolution fidelity: agentic synthesis serves the effective prompt; prompt-boundary and entity-normalization gaps closed (#884, #925)

## What changed

- **Agentic RAG serves the effective prompt (#884).** The agentic branch of
  `RAGEngine.query` now resolves the per-query effective prompt through the
  SAME A/B-aware entrypoint as the standard pipeline
  (`_resolve_prompt_with_ab_sync` with the query's exact `vault_id`/`user_id`),
  and threads it into `SynthesisTool(system_prompt=...)`. An admin-activated
  version, an org override, and an assigned A/B variant are all served on the
  agentic path now; the agentic done event gains the same
  `prompt_version`/`ab_experiment_id`/`ab_variant` metadata the standard
  pipeline's done event carries. When nothing resolves, the tool serves its
  built-in agentic prompt byte-for-byte (the documented resolution order's
  built-in tier for this pipeline).
- **Composition preserves the security and citation contract (#884).** A
  served override is completed TAG-SPECIFICALLY: any agentic wrapper tag it
  does not declare (`<user_query>`, `<source_passages>`) gets the standard
  SECURITY BOUNDARY sentence, and the established citation instruction is
  appended when the override carries none; citation_mode disabled/required
  semantics mirror `build_messages`.
- **Planner boundary wording (#925).** `QueryPlanner.plan`'s system prompt
  now carries the full SECURITY BOUNDARY clause (same wording family as the
  follow-up/step-back/HyDE prompts) instead of a bare "treat as literal
  data" rule.
- **Declaration audit (#925).** `missing_tag_declarations()` checks that a
  prompt names every builder-emitted wrapper tag inside its SECURITY
  BOUNDARY clause (prose mentions do not count). A WARNING fires on BOTH
  activation paths (`PromptVersionStore.activate` and
  `create_version(..., activate=True)`) and at serving time when an emitted
  tag is undeclared (rate-limited once per process per prompt/tag). The
  built-in default declares everything and never warns.
- **History wrapping (#925).** Chat-history turns render inside a declared
  `<user_message>` wrapper (escaping stays inside the wrapper, so a raw
  `</user_message>` payload in a prior turn cannot break out).
- **Entity-safe context budgets (#925).** `rewrite_followup` clips the
  ESCAPED prior-question/answer regions to their documented budgets
  (1000/1500 chars) via the new `_escape_clip` helper (trailing partial
  entities are stripped); the untrusted region can no longer inflate ~6x
  past the budget.
- **Entity normalization at every retrieval seam (#925 + defect-class
  siblings).** `rewrite_followup` unescapes the model's rewrite BEFORE
  quote-stripping (a compliant verbatim echo of the escaped reference copy
  reaches retrieval as raw text); retained planner sub-queries, the
  step-back variant, and the agentic planner's `retrieve_more` sub-queries
  are all unescaped before use. HyDE is unchanged: it asks for a new
  passage rather than a verbatim echo, so no echo contract is violated
  (documented disposition).
- **Header separator normalization (#925).** `_header_escape` normalizes
  U+2028/U+2029, vertical tab, form feed, NEL, NUL, and the ASCII
  information separators U+001C-U+001E in addition to CR/LF, across all
  consumers (wiki/KMS/document filename/section/context headers, the chat
  history wrapper, and the multimodal escaper).
- **Serving-audit hardening (review pr1105-20261109).** The audit's dedupe
  key is a digest of the full served prompt (the earlier 120-char prefix
  let different prompts sharing a preamble suppress each other's warnings);
  a clause naming a tag without marking it untrusted declares nothing; the
  audit covers `<visual_observation>` and fires for an empty effective
  prompt.
- **Cache identity (review pr1105-20261109 PRR-002).** `PROMPT_VERSION`
  bumps to v3: the planner prompt changed and cached plan/step-back payloads
  changed semantics (retained values are unescaped before use), so v2 cache
  entries stop serving at deploy (one-time invalidation by design).
- **A/B exposure on the agentic path.** Agentic queries now record A/B
  experiment exposures like the standard pipeline always did (idempotent
  INSERT OR IGNORE; exposure stats include agentic traffic).

## Why

Admins could activate prompt versions and org overrides that silently never
reached agentic answers (#884), and the prompt surface carried one-sided
transformations: undeclared tags, entity-encoded retrieval strings, unbounded
escaped context, and separator-level header spoofing (#925). Found in the
#708 trace (negative boundary) and the PR #919/#711 review rounds.

## Tests

`backend/tests/test_prompt_resolution_fidelity_884_925.py` (49 tests):
per-AC regression + negatives (built-in literal pin, resolution-failure
fallback, partial-boundary + prose-mention + inverted-clause completion,
citation modes with overrides, declaration near-miss, warning dedupe incl.
prefix-collision isolation, named and numeric entity-clip boundaries,
org-override precedence, A/B challenger content + done-event metadata,
resolver parity and exact arguments, the sibling seam guards — step-back,
prior-user region, evaluator, chat-title seed via the production helper,
agentic sub_query incl. non-string fallback — and the escaped-budget pins
for the synthesis evidence passages, the multimodal wrap/query, and the
chunk context header). Review rounds also extended the repo-wide
test_c06_service_escape_ast_census guardrail to recognize `_escape_clip`
(with the exact-escaper paired pins) and added a positive control to the
query-transformer interpolation census.

## Security notes

- The bandit baseline was re-anchored for line shifts only: findings count
  is unchanged (128) with an identical finding multiset (no new
  suppressions).
- Known limitations: `_escape_clip` strips a trailing alphanumeric run
  after a literal `&` at the clip boundary (bounded to 32 chars); the
  declaration audit's clause is paragraph-scoped by design (a tag named
  outside the SECURITY BOUNDARY paragraph does not count); agentic
  fallback to the standard pipeline resolves the prompt a second time
  (bounded, opt-in via `AGENTIC_RAG_ENABLED`).
