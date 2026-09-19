# Model Research Verification — 2026-09 (issue #36 / MODEL-RESEARCH-01)

Primary-source verification of the models and claims used by the 2026-09
qualification, executed 2026-09-19 (deployment: R640AI, commit 281bd714, TEI
1.9.3 sha 4150561, harrier-oss-v1-0.6b + bge-reranker-v2-m3). External
ranking numbers that could not be re-verified against a primary benchmark in
this pass are labeled as such; the decision evidence for this PR is the
frozen-pool A/B on the real vault, not leaderboard numbers.

## Harrier query/document formatting

Verified against the model card (huggingface.co/microsoft/harrier-oss-v1-0.6b,
accessed 2026-09-19): harrier-oss-v1 is a decoder-only family with
**last-token pooling and L2 normalization**; the 0.6b variant is 1024-d,
32,768 max tokens, MTEB v2 69.0 (the frontier-audit claim "≈69.0" checks out).

- **Documents**: "No need to add instruction for retrieval documents" — the
  deployed index was built with no document prompt, which is correct.
- **Queries**: "Each query must come with a one-sentence instruction that
  describes the task"; the card's retrieval usage wraps every query as
  `Instruct: {task}\nQuery: {query}` (sentence-transformers prompt name
  `web_search_query`). The exact E5-style instruction string is corroborated
  by huggingface.co/thinletter/harrier-0.6b-query-clients (accessed
  2026-09-19): "Instruct: Given a web search query, retrieve relevant passages
  that answer the query\nQuery: …", with EOS appended by the tokenizer.
- Deployment cross-check: TEI `/info` on the R640 reports `last_token`
  pooling, fp16, max_input_length 16384 (half the card's 32,768 — an
  operational truncation point worth knowing, not a defect), and the app's
  live `embedding_query_prefix` setting is EMPTY: queries are embedded
  WITHOUT the card-required instruction. This is the deployment gap the
  E05 prefix-only A/B measures (see 2026-09-model-qualification.md).

## Harrier query prefix

The model-card-mandated query prefix is missing on the deployment
(`EMBEDDING_QUERY_PREFIX` unset; the app only auto-applies prefixes for Qwen
model names — backend/app/services/embeddings.py:304-318, constants at
184-190). The prefix A/B on the frozen 55-query set measured: gold-recall@7
0.927 → 0.964 (+0.036, below the pre-declared 0.05 bar), rank-1-gold 32/55 →
38/55, MRR 0.730 → 0.799 (+0.069). Direction is consistently positive but
under the bar, so this PR ships no default change; the recommendation
(recorded in the release note) is a controlled deployment trial with
`EMBEDDING_QUERY_PREFIX` set to the card instruction, followed by a fresh
calibration replay if adopted. Query-prefix changes require no re-embed (the
document index is prefix-free and stays compatible — per the card and the
thinletter compatibility note).

## Challenger verification

- **Qwen3-Reranker family not TEI-servable (verified claim)**:
  github.com/huggingface/text-embeddings-inference/issues/643 (2025-06-17)
  shows TEI failing to load Qwen3-Reranker ("`classifier` model type is not
  supported for Qwen3"); github.com/huggingface/text-embeddings-inference/issues/691
  (2025-08-04) requests the same support. As of the deployed TEI 1.9.3
  (accessed 2026-09-19) this remains unresolved, matching the frontier-audit
  note. The challenger for the frozen-pool A/B was therefore served via
  llama.cpp (`ghcr.io/ggml-org/llama.cpp:server-cuda --rerank`,
  mradermacher/Qwen3-Reranker-0.6B-GGUF:Q8_0, GPU 0) — labeled
  `comparability=ad-hoc-gpu` per the pre-declared rule.
- **Qwen3-Reranker-0.6B on the real vault**: card at huggingface.co/Qwen/Qwen3-Reranker-0.6B
  (accessed 2026-09-19) documents a causal-LM yes/no-logit reranker (32k
  context, 100+ languages). Measured on the frozen pools: gold-in-top-7
  0.896 vs deployed bge-reranker-v2-m3 0.979 (delta −0.083, beyond the 0.05
  noise floor; 7/55 challenger requests errored HTTP 500 and are retained as
  failures). REJECTED for this deployment; the frontier-audit's external
  ranking numbers for this model were not reproducible here.
- **Qwen3-Embedding-0.6B/4B (frontier-audit claims ≈70.5 / ≈74.6 MTEB v2)**:
  not re-verified against a primary benchmark in this pass (leaderboard
  access not exercised); treated as experiment leads per the frontier audit's
  own caveat. Dimensional facts that gate any future trial: 0.6B is 1024-d
  (same as Harrier — index-compatible without a dimension rebuild), 4B is
  2560-d (requires the staged dimension rebuild, backend/app/services/
  vector_store.py:2174-2315). The app would auto-apply its domain-customized
  Qwen prefixes (embeddings.py:184-190: "Represent this technical
  documentation passage for retrieval" / "Retrieve relevant technical
  documentation passages") — note these are NOT the Qwen card's default
  instructions, a deliberate domain adaptation that any future trial should
  A/B explicitly.
- **bge-reranker-v2-m3 (deployed baseline)**: served by TEI with
  raw_scores=true and a single client-side overflow-protected sigmoid —
  the #511 score contract; measured gold-in-top-7 0.979 on the frozen pools,
  the strongest reranker measured on this corpus.
- **Harrier provenance corroboration**: techcommunity.microsoft.com
  "Now in Foundry: Microsoft Harrier and NVIDIA EGM-8B" (2026-04-13)
  confirms the decoder-only, task-instruction-query architecture.

## Source list

1. https://huggingface.co/microsoft/harrier-oss-v1-0.6b — accessed 2026-09-19
2. https://huggingface.co/thinletter/harrier-0.6b-query-clients — accessed 2026-09-19
3. https://github.com/huggingface/text-embeddings-inference/issues/643 — accessed 2026-09-19 (issue dated 2025-06-17)
4. https://github.com/huggingface/text-embeddings-inference/issues/691 — accessed 2026-09-19 (issue dated 2025-08-04)
5. https://huggingface.co/Qwen/Qwen3-Reranker-0.6B — accessed 2026-09-19
6. https://techcommunity.microsoft.com/blog/azure-ai-foundry-blog/now-in-foundry-microsoft-harrier-and-nvidia-egm-8b/4510851 — accessed 2026-09-19 (published 2026-04-13)
