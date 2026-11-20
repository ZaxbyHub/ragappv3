# fix(draft-room): project-owned outline mode, projected prompts, gated evidence, per-call telemetry, and deterministic quality gates

## What changed

Issues #1149 and #1150 (Workstream A PRs 1-2 of 3, 2026-11-15 deployment audit), one PR:

- **Mode is the project's, not the model's** (#1149): the outline provider schema
  (`OutlineModelOutput`) no longer has a `mode` key; the pipeline stamps the
  persisted `OutlineArtifact.mode` from the compile context. (The bundle-version
  bump means pre-fix checkpoints are deliberately not reused; the reuse path
  still defensively re-stamps any artifact whose recorded mode disagrees with
  the project.) `outline_empty` stays as the mode-independent backstop.
- **One brief-projection layer** (#1149): compose-mode compiles never send
  `transformation_strength` to any model; the Draft stage renders a
  section-scoped brief (`section_target_words` /
  `section_evidence_labels`; the planned heading and purpose ride the
  projected upstream artifact, not the trusted brief) that excludes
  `acceptance_checks`; continuity text is
  labelled "already covered — do not repeat"; rendered prompts are never
  logged at any level — the per-call DEBUG line carries content-free
  identifiers only (stage, sha256, length), per SPEC §9.2.
- **Typed evidence gating** (#1149): vault evidence is de-duplicated by
  content hash (filter-before-mark ordering) and gated by a per-score-kind
  normalized relevance (`RetrievedSource.score_kind`); raw-BM25 rows gate on
  their own configured scale; unknown kinds are never gated. Project inputs
  stay exempt.
- **Per-call telemetry + true stage timing** (#1149): every provider call
  (structured stages, research, repair passes) records wall-clock duration
  and token counts when the provider reports usage (or the client's
  documented estimate, labelled as such) - `ProviderCallRecord`, transported
  on `CompletionResult` from the production streaming seam; calls whose
  provider reports no usage honestly record `token_source="unavailable"`.
  Every stage row opens at stage entry (after its checkpoint-reuse check)
  and closes on success, failure AND cancellation on every in-process
  path - the terminal settle retries a write interrupted once, while the
  late-open completion path makes a single settle attempt (both log and
  disclose a persistent write failure, which leaves the row open; no
  sweep recovers a terminal job's stage rows) - and no more same-second
  started/completed stamps on the work path.
- **Deterministic quality gates** (#1150): length (blocker outside ±25%,
  advisory outside ±15%), section-over-budget (1.5x + absolute floor),
  duplicate paragraphs (hash + word-set similarity), cross-section duplicate
  sentences, multiple H1 / heading-repeats-title, process-commentary leakage
  and a trailing source line are blocker-severity findings versioned by
  `draft_quality_rule_version`; `must_preserve` spans (all occurrences) are
  exempt; `unsupported` claims and Lint blockers enter the bounded correction
  loop; the Ready gate and waiver mechanism cover the new blockers with
  per-finding rule versions.
- **Checkpoint invalidation**: `PROMPT_BUNDLE_VERSION` bumped
  (`2026.11.19-draft-prompts-4`) — pre-fix checkpoints were produced without
  the gates, projections and stamping and are deliberately not reusable; the
  evidence-gate and quality-rule versions join their stage input hashes.
- New settings `draft_evidence_min_score` (0.05), `draft_evidence_min_fts_score`
  (0.5), `draft_quality_section_floor_words` (40), `draft_quality_rule_version`
  (1), delivered through `.env.example`, `docker-compose.yml`, the config
  contract's typed parity maps and SPEC §15. SPEC §11.8/§13.1/§13.3 amended.

## Why

The 2026-11-15 deployment audit watched a compose project outlined as a
rewrite with zero sections, outline acceptance-check text transcribed into
prose as "verification was not performed", one document labelled five times
down to score 0.0000, a 602-word self-duplicating revision approved against a
200-word target, and stage rows whose start time equalled their completion
time. #1147's guards failed closed on the symptom; this PR removes the causes.

## Operator notes

- New knobs (all optional, safe defaults): `DRAFT_EVIDENCE_MIN_SCORE` (0.05),
  `DRAFT_EVIDENCE_MIN_FTS_SCORE` (0.5), `DRAFT_QUALITY_SECTION_FLOOR_WORDS` (40),
  `DRAFT_QUALITY_RULE_VERSION` (1). Raising the evidence floors drops more
  retrieved passages from Draft evidence; the quality-gate version should be
  bumped when gate rules change so new findings are distinguishable.
- `PROMPT_BUNDLE_VERSION` bumped: in-flight compiles at upgrade time fail the
  resume gate cleanly and restart from intake (documented intent — pre-fix
  checkpoints were produced without the gates, projections and stamping).
- The word tokenizer for the length/duplicate rules is now Unicode-aware;
  non-Latin drafts are measured (previously they would all have been blocked
  as zero-word candidates).

## Rollback

Revert the PR. In-flight jobs then re-use their pre-fix checkpoints again
(the bundle version reverts); the four new settings are additive with safe
defaults and can remain in `.env`. No database migration involved.
