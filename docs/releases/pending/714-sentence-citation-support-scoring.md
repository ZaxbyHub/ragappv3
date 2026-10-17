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
  `content` (rendered inline by `PromptBuilderService.build_messages`), wiki
  `claim_text or excerpt` (`prompt_builder.format_wiki_evidence`), KMS
  `excerpt or summary` (`prompt_builder.format_kms_evidence`). Registry
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

Every family's rendered block embeds the label token itself (`[W1] Title…`,
`[K1] Title…`, `[M1] <memory>…`), and the tokenizer strips punctuation —
scoring the full rendered block would put token `w1`/`m1` in both the claim
and evidence sets and force overlap > 0 for every citation, structurally
breaking the zero-overlap contract. Full rendered-evidence parity (headers
and escaping included) remains #716's scope.

## Migration steps

None. The new backend argument is optional with a default of `None`; existing
callers keep their exact behavior. Frontend consumers of `citationConfidence`
see a key superset: per-label lookups are unaffected and SourceCards'
Cited/Retrieved derivation is unchanged for answers that cite `[S#]` sources
(answers citing only non-S labels render no source cards at all).

## Known limitations

- Support scores are lexical-overlap diagnostics, not verified grounding; the
  UI labels them as such everywhere they appear.
- Scoring splits the answer into sentences on `.!?` boundaries only. A label
  placed after the sentence period scores against the bare label fragment
  (0.0), and labels inside an unpunctuated block (bullet or table lines with
  no terminal punctuation) all score against that one block, so a label can
  inherit overlap from a neighboring bullet's words — pre-existing
  sentence-split semantics, identical for `[S#]`; unchanged here.
- Registry scores are computed against the evidence body text (unescaped),
  while the prompt renders that body XML-escaped; bodies containing `&`, `<`,
  `>` or quotes tokenize slightly differently than the escaped form the model
  saw. The same raw-vs-rendered divergence already exists on the `[S#]` path;
  full rendered-evidence parity is #716's scope.
- MarkdownMessage inline `[W#]/[M#]/[K#]` chips do not show scores (the issue
  names the citation cards as the UI surface); the done payload carries all
  families' scores for any future consumer, and the RightPane evidence tabs
  pass no scores yet (#965).
