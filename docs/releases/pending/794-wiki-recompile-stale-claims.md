# fix(wiki): recompiling an edited document no longer leaves old claims active (#794)

## What changed

When a file is re-uploaded or rescanned, it keeps its `file_id` and its
`parsed_text` is replaced in place; the ingest wiki job then recompiles the
file against the NEW text. The deterministic claims pass only ever
find-or-creates claims for sentences present in the current text — nothing
superseded claims whose supporting sentence had disappeared, because
`WikiStore.mark_claims_stale_by_file` was wired only to the file-delete
paths. The removed sentence's claim stayed `status='active'` forever, and
wiki retrieval (which serves active claims only) kept surfacing it next to
the new, contradictory claim.

- **Recompile now retires un-re-derived claims.**
  `WikiCompiler.compile_ingest_job` calls the new
  `WikiStore.supersede_unrederived_file_claims` after the deterministic
  pass. Candidates are claims with a document source for this file, bound to
  this file's page, in this vault, currently `active`. A claim is kept when
  it was re-derived this run (including normalized-text reuse), or when any
  of its document-source quotes for this file still appears in the current
  text (exact, then the same normalization behind `find_claim_by_normalized_text`).
  A claim with no non-empty quote is undecidable and fails open — absence of
  evidence never supersedes.
- **Page scoping protects query-synthesis claims.** Claims created by the
  chat-answer (`compile_query_job`) path carry no page binding and quote the
  synthesized answer sentence rather than a file span; they are never
  candidates, so even a byte-identical recompile changes nothing for them.
  Curator claims keep their verbatim `source_quote`, so paraphrased
  `claim_text` survives recompiles while its quote exists.
- **Delete-path policy split applies.** Sole-source claims are set
  `superseded` plus one `stale` lint finding; multi-source claims stay active
  with one `weak_provenance` finding (the delete-path rule). Findings use
  per-claim titles and an open-row existence check, so repeated recompiles
  never duplicate finding rows.
- **Docstring corrected.** `mark_claims_stale_by_file` (and the memory
  sibling) no longer promise `status='stale'` — a value the `wiki_claims`
  CHECK forbids; the code writes `superseded`, and now the docstrings say so.

New regression coverage: `backend/tests/test_o04_wiki_recompile_supersede.py`
(frozen acceptance checks: compile v1 Alice/Carol → recompile v2 Bob/Carol
leaves `(alice_active, carol_active) == (0, 1)`, and the docstring pin) plus
`backend/tests/test_o04b_recompile_supersede_policy.py` (11-test policy
battery: multi-source split + single-finding bounds, quote-presence keeps for
extraction-skipped and case-variant sentences, byte-identical recompile
no-op, query-shape and curator-paraphrase preservation, empty-text no-wipe,
non-active and cross-vault untouched, empty-quote fail-open).

Residual (disclosed): the fix's coverage equals the compile job's coverage —
if the wiki ingest job is never enqueued (wiki compile flags off) or dies
before this step, staleness persists until the next successful compile, same
as the delete-path invalidation hook. Claims with a foreign/NULL page binding
whose only sources are files are also out of reach of the page-scoped step
(the price of excluding query-synthesis claims).
