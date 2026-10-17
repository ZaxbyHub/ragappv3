# feat(citations): per-sentence support scoring for every label family (#714)

## What changed

`score_citations` computed a Jaccard-overlap support score per `[S#]` (document)
citation only; a claim citing `[M3]`, `[W2]` or `[K1]` was never checked for whether
the cited text actually supports the sentence. This extends per-sentence support
scoring to all four label families and surfaces it in the done payload and the
citation cards (Workstream C PR 9 of 14, audit ENH-06 enhancement half).

- **Per-family support registry.** `score_citations` gains an optional
  `support_texts_by_label` mapping (label → rendered evidence body). The RAG
  engine builds it from the evidence actually cited in the answer — memory
  `content`, wiki `claim_text or excerpt`, KMS `excerpt or summary` (the same
  body precedence `prompt_builder.format_*_evidence` renders). Registry
  membership is the validity gate: the registries hold only offered-and-cited
  evidence, so phantom labels never score (building on PR 4's label-membership
  validation, #709/#890).
- **Done payload carries every family.** `citation_confidence` on the done
  chunk (and the non-streaming `ChatResponse`) now includes `M#`/`W#`/`K#`
  keys alongside `S#`. Zero token overlap scores exactly `0.0` — flagged, not
  omitted; an empty evidence body also scores a natural `0.0`.
- **Support chips on citation cards.** Wiki, memory and KMS cards render a
  `Support NN%` chip when the done payload carries a score for their label,
  with a tooltip stating it is a word-overlap heuristic ("a lexical measure,
  not a correctness probability") — never "verified". The wiki card's existing
  bare `NN%` display (the claim's own curation confidence) is unchanged and
  visually distinct.
- **Unchanged surfaces (pinned by tests).** The `[S#]` Jaccard values and
  thresholds (including the 6/11 France/Paris case), the document-only
  `unverifiable_claims` rule, `valid_citations`/`has_any_citation` result
  fields, and the SourceCards Cited/Retrieved badge derivation are all
  byte-identical.

## Why bodies, not full rendered blocks

The rendered wiki/KMS headers embed the label token itself (`[W1] Title…`), and
the tokenizer strips punctuation — scoring the full rendered block would put
token `w1` in both the claim and evidence sets and force overlap > 0 for every
citation, structurally breaking the zero-overlap contract. Full
rendered-evidence parity remains #716's scope.

## Migration steps

None. The new backend argument is optional with a default of `None`; existing
callers keep their exact behavior. Frontend consumers of
`citationConfidence` see a key superset (SourceCards only queries its own
`[S#]` labels).

## Known limitations

- Support scores are lexical-overlap diagnostics, not verified grounding; the
  UI labels them as such everywhere they appear.
- A citation label placed after the sentence period scores against the bare
  label fragment (0.0) — pre-existing sentence-split semantics, identical for
  `[S#]`; unchanged here.
- MarkdownMessage inline `[W#]/[M#]/[K#]` chips do not show scores (the issue
  names the citation cards as the UI surface); the done payload carries all
  families' scores for any future consumer.
