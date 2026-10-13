# fix(draft): checkpoint resume survives corrupted artifacts, persisted-JSON rot is observable, desk prompts stop asking for what models cannot compute

Issue #787 (Workstream N PR 3 of 6). Audit findings MUT-GX-026, TQ-sweep-B06-04, TQ-sweep-B02-01, TQ-instruction-batch-4-01/02/03.

## What changed

**Compile checkpoint resume (draft_pipeline.py)**
- All eight stage checkpoint-reuse branches now deserialize their persisted artifact through one shared guarded helper (`_load_checkpoint`): a checkpoint whose stored `artifact_json` no longer validates against the current schema is discarded with a WARNING and the stage re-runs, instead of terminally failing the resumed job with `internal_error`.
- A parametrized regression test drives corruption→re-run for every stage (intake/research/outline/draft/lint/copy/standards/fact), and an AST census guardrail keeps any future unguarded `reused.artifact_json` deserialization out of the module.
- Two new tests kill mutation GX-026: `_stage_input_hash`'s upstream-artifact fold (`**payload`) is pinned at the unit level and at the resume level (a changed upstream research packet forces the outline stage to re-run with world/bundle/fingerprint fixed). `scripts/check_n03_gx026_mutant.py` (committed) re-proves the kill mechanically: it applies the exact mutant to a disposable copy of `backend/` and asserts the suite now fails.

**Persisted-JSON observability (draft_store.py)**
- `get_job_json_field` now logs a WARNING naming the job id and field when a stored compile JSON body is undecodable or not an object, while keeping the documented `{}` return for genuinely-absent values.

**Desk prompt contracts (draft_prompts.py / draft_research.py / draft_pipeline.py)**
- Copy and Standards prompts no longer ask the model for per-edit character offsets or SHA-256 digests (a language model cannot compute them); they identify the span by `before_excerpt` and the pipeline computes offsets/hashes server-side. `CopyEdit` offsets/hashes are now optional with neutral defaults.
- `_apply_edits` locates edits by their `before_excerpt` whenever the supplied offsets/hashes do not verify (wrong hashes, garbage offsets, or omitted), applying uniquely-matching edits with server-computed pins and surfacing unlocatable edits (zero or multiple occurrences) as `draft_findings` rows instead of silently dropping them.
- The Research desk is asked only for `contradictions` and `gaps` — the only fields `run_research` keeps from the model response. The template, the parsed model, and the provider-side `response_format` schema are narrowed together; the persisted `ResearchPacket` artifact shape is unchanged.
- The Fact desk prompt states its real contract (classify against the provided evidence registry; the pipeline downgrades sourced verdicts whose evidence labels are not snapshotted, then re-searches every factual claim for its own audit record) and stops asking the model to fabricate a `retrieval_audit` object whose `vault_scope_hash` the desk cannot know; `_resolve_claims` no longer persists model-supplied audits for non-factual claims (factual claims keep their deterministic pipeline audit).

## Why

All six audit findings share one theme: a persisted or model-supplied artifact silently diverging from what its consumer assumes. A corrupted checkpoint crashed resumed compiles instead of using the re-run path sitting beneath the unguarded deserialize; JSON rot was indistinguishable from absence on every read; three desks were asked for SHA-256 digests, hash-consistent re-emissions, and retrieval audits they cannot produce — and the pipeline silently discarded their best-effort edits when the uncomputable pins did not verify.

## Migration

No schema migration. Old compile checkpoints for research/copy/standards/fact no longer match after upgrade (their prompt hashes changed). Those stages re-run once on the next resume — the designed invalidation path — and because every stage input hash folds its upstream artifacts, a research re-run whose packet differs also cascades into outline/draft/lint/fact, i.e. the practical cost after upgrade is one full pipeline re-run from Research. Persisted copy/standards stage artifacts gain one additive, server-populated `unapplied_edits` key; rows written before it still validate.

## Caveats

- An edit whose `before_excerpt` matches multiple locations in the candidate is surfaced as a finding, not applied (conservative; the pipeline cannot know which occurrence the desk meant).
- A no-op edit (replacement identical to the current span text) remains silently skipped — nothing is lost; the requested end state already holds.
- `scripts/check_n03_gx026_mutant.py` is a manual certification instrument (two disposable `backend/` copies, two pytest runs), not a CI gate; the ongoing regression guard is the killing tests themselves, which run in the Backend CI job.
- Non-factual claims (quote/opinion) now persist a null `retrieval_audit`; previously they persisted whatever the model fabricated.
