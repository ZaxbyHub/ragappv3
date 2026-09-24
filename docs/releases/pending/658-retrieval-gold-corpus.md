# Deterministic retrieval gold corpus (sha-pinned fixtures + contract tests) (Issue #658)

## What changed

- `backend/tests/fixtures/retrieval_gold/` — a new synthetic, license-safe
  corpus of 10 plain-text documents on deliberately confusable topics (a
  superseded specification revision pair sharing a verbatim preamble, a
  competing vendor specification, a distributor bulletin contradicting the
  controlling spec, a memo quoting it, plus timeline/glossary/permit/opinion
  documents), with a `manifest.json` whose sha256 values are computed over
  **CRLF-to-LF normalized** bytes (`normalization.policy: crlf-to-lf`) so the
  corpus holds identically on Linux CI and Windows/CRLF checkouts, and 25
  pinned query/expected-span cases (`cases.json`) — 15 near-trap cases (14
  with text-proven trap spans), one not-in-corpus case.
- `backend/tests/retrieval_gold/gold_corpus.py` — a test-support loader with
  full validation (normalized hash parity, verbatim spans at recorded
  offsets, trap rules, the probe-case identical-offsets guarantee, and a
  cross-document chunk tie guard). It imports nothing from `backend/app`;
  the purity is enforced by a test.
- `backend/tests/test_retrieval_gold_corpus.py` — four contract families:
  manifest parity (including a CRLF-rewrite robustness test), case
  integrity, loader contract with a 17-proof tamper family (`pytest -k tamper`),
  and a retrieval-discrimination family (`pytest -k discrimination`) that
  runs the repo's REAL retrieval path — LanceDB dense + BM25 FTS hybrid
  search, RRF fusion, the production relevance cutoff and dedup — fully
  offline in ~8s, using a deterministic token-hash embedding at the
  `embedding_service` seam with reranking and query transformation pinned
  off and FTS participation asserted.
- Docs: `docs/engineering/eval-closure-checklist.md` records the corpus as
  met at HEAD (25 deterministic cases in CI) while the operator-labeled
  real-corpus split remains pending; `docs/eval-operator-workflow.md` §3a
  states what the corpus proves and does not prove;
  `docs/engineering/testing.md` now documents the observed 44-item Windows
  environmental baseline.

## Why

Retrieval quality had no deterministic CI signal: every existing input is
operator-labeled (0 labeled cases), judge-gated ("never in CI"), flag-gated
(501 without `EVAL_ENABLED`), or network-dead in nightly, so a ranking
regression could ship green. This corpus is the prerequisite the 2026-09-22
frontier audit identified (E7) for any future retrieval-quality floor.

## Usage

No user-facing surface changes. The suite runs automatically in the Backend
CI job. To iterate on it locally:
`cd backend && python -m pytest tests/test_retrieval_gold_corpus.py -q`.
`RETRIEVAL_GOLD_ROOT` redirects fixture resolution in the loader (used by the
frozen ranking-degradation probe). `manifest.json` and `cases.json` are
generated artifacts: regenerate them byte-stably from the hand-authored
documents with
`python backend/tests/retrieval_gold/generate_corpus.py`.

## Known limitations

The discrimination family substitutes a deterministic token-hash embedding
for the live embedding provider, so synonym/paraphrase retrieval is out of
reach by construction; the harness also substitutes its own paragraph
chunker for `SemanticChunker`, pins reranking and query transformation off,
and asserts ranking at passage level. What the corpus does and does not
prove is enumerated in `docs/eval-operator-workflow.md` §3a. Answer quality
and calibrated relevance still require the operator-labeled real corpus and
the human calibration panel (both outside this change). A nightly
retrieval-quality floor built on this corpus is deliberately NOT part of
this change (tracked as the follow-up in the issue's Definition of done).
