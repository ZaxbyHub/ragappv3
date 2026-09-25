# Unwired deprecated settings removed: `tri_vector_search_enabled` and `flag_embedding_url` (Issue #614)

## What changed

- `backend/app/config.py` drops the two Pydantic `Settings` fields:
  `tri_vector_search_enabled: bool = False` and `flag_embedding_url: str = ""`.
  Both were already documented in source as `Deprecated: BGE-M3 replaced by
  Harrier` and `Deprecated: FlagEmbedding server removed`, and the Phase 4.2
  recurrence sweep of the #202 resolution verified they had no production
  consumer under `backend/app` (attribute-form and `getattr`-string-form census
  on 255 `Settings` fields). After this removal the sweep's remaining
  unwired operator-facing class is empty — `recency_decay_lambda`,
  `retrieval_profile`, `sparse_embedding_timeout`, `sparse_search_max_candidates`
  are internal-only (not in `.env.example`) and out of the operator-facing class.
- `.env.example` drops the `# Phase 6: Tri-Vector Search (DEPRECATED — replaced
  by Harrier dense embeddings)` block: `TRI_VECTOR_SEARCH_ENABLED=false` and
  `FLAG_EMBEDDING_URL=`.
- `docker-compose.yml` drops the two literal entries (`TRI_VECTOR_SEARCH_ENABLED`
  and `FLAG_EMBEDDING_URL`) from the `knowledgevault` service `environment:`
  block so the compose contract no longer carries keys the app cannot read.
- `backend/tests/test_config_alignment.py` drops the five assertions that
  pinned the removed fields' defaults and env-var toggle
  (`test_tri_vector_search_enabled_default_false`,
  `test_flag_embedding_url_default`,
  `test_tri_vector_search_enabled_is_false`,
  `test_feature_flag_tri_vector_toggle`,
  `test_flag_embedding_url_empty_allowed`). Their absence is now an intentional
  consequence of the schema change — the same fix that deprecated the fields
  deprecated their tests.
- Test fixtures that touched the deleted attrs on mock settings now omit them
  (no behavior change — `MagicMock` accepts any attribute):
  `test_embeddings_cache.py`, `test_rag_engine_hybrid_status.py`,
  `test_retrieval_top_k_migration.py`, `test_llm_integration.py`,
  `test_adversarial_security.py`, and `test_issue494_compose_literal_values.py`
  (which removed `TRI_VECTOR_SEARCH_ENABLED` and `FLAG_EMBEDDING_URL` from the
  compose-literal whitelist alongside the compose-block edit).

## Why

- Issue #614 reports these two fields as the actionable residual of #202's
  ENH-012 defect class (`declared-but-unwired configuration`). Operators
  setting `TRI_VECTOR_SEARCH_ENABLED=true` or `FLAG_EMBEDDING_URL=...` got no
  behavior change and no error — exactly the failure mode #202 was created
  to root out.
- Both fields' docstrings already declared the features dead (`BGE-M3
  replaced by Harrier`, `FlagEmbedding server removed`). Implemented by
  removal rather than by wiring because there is no live consumer to wire
  — `VectorStore.search`, the dense embedding service, and the hybrid
  retrieval path have all moved to Harrier/BGE and never read these.
- Keeping the fields' only purpose today is to mislead an operator who
  skims the example env file. Removing them shrinks the operator-facing
  surface to fields that actually do something.

## Migration steps

- For self-hosted operators: delete any `TRI_VECTOR_SEARCH_ENABLED=...` or
  `FLAG_EMBEDDING_URL=...` line from your `.env` / compose override. After
  upgrade the app's `Settings()` will reject the unknowns only if
  `extra="forbid"` is enforced — Pydantic-settings here uses
  `extra="ignore"` (the project convention), so unrecognized keys are
  silently dropped. No startup failure either way; the operator can
  confirm removal with a grep, not a deploy.
- For compose deployments: the `knowledgevault` service block no longer
  pins `TRI_VECTOR_SEARCH_ENABLED` or `FLAG_EMBEDDING_URL`. Any override
  file that sets them will be passed through unchanged but ignored by
  the backend; remove them at the same time as the image upgrade for
  hygiene.
- For downstream tooling: any external dashboard/admin docs that document
  these flags should drop the row. No public API changes.

## Known caveats

- If a future feature ever does want a tri-vector or FlagEmbedding code
  path, it must be added back as a NEW field with a NEW owner and a
  NEW `.env.example` block — not by un-deleting these names, because
  prior operators may have moved on.
- The Phase 4.2 sweep recorded `recency_decay_lambda`, `retrieval_profile`,
  `sparse_embedding_timeout`, and `sparse_search_max_candidates` as
  internal-only (not exposed in `.env.example`). ~~They are deliberately
  retained; if an operator-facing need arises they should graduate via the
  same wire-then-test pattern that #202 / #615 used for `JWT_ALGORITHM`.~~
  **Superseded by #662**: all four were later verified to have no production
  consumer at all (none wire-able: the sparse pair's code path was removed by
  the Harrier migration, `recency_decay_lambda`'s exponential formula was
  never implemented, `retrieval_profile` was superseded by its own wired
  boolean) and were removed with named-field reintroduction guards. The
  #662 settings-consumer census (`scripts/check_settings_consumers.py`, wired
  into CI) now makes the declared-but-unwired class fail the build at
  authoring time.
- No bandit or SAST findings involve these two identifiers — the deletions
  are pure deletion, no callsite touched, so `backend/security/bandit-baseline.json`
  is unchanged.
