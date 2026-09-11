# Draft Room evidence-complete editorial checking (Issue #517, Workstream D3)

## What changed

### Backend — editorial findings, evidence and verification honesty
- **Desk findings reach the review list.** Copy and Standards desk findings
  were previously trapped inside stage artifacts; they are now normalized
  into `draft_findings` rows (stable `<stage>.desk_finding` identity,
  advisory severity, waivable, no invented span), including when a stage is
  replayed from a checkpoint. DRAFT-008.
- **Bounded Copy/Standards convergence loop.** A semantic or structural
  Standards edit now re-runs the Copy desk before Fact, and a Copy pass that
  changes text goes back to Standards, stopping at `DRAFT_QA_RETRY_LIMIT`
  with residual findings visible (SPEC §11.7 steps 2-4). DRAFT-009.
- **Paraphrased unsupported claims stay adverse.** A claim whose proposition
  cannot be located verbatim in the candidate is retained in the claim
  ledger without an invented span; an adverse verdict (unsupported /
  contradicted / ambiguous / stale) keeps blocker severity instead of
  degrading to a warning, so it can never ship as a passed revision with an
  empty-looking ledger. DRAFT-010.
- **Claim-specific retrieval for every factual claim.** Supported factual
  claims now receive the claim-specific contradictory/newer-evidence search
  SPEC §11.8 promised (with a job-scoped validated cache keyed by
  proposition, vault, source snapshot and retrieval config), and the
  retrieval audit is persisted on the claim row either way. DRAFT-012.
- **Correction retries carry the feedback.** When Fact requires a
  correction, the Copy/Standards retry prompts now include the Fact findings
  and the approved outline (new `correction_feedback` prompt block;
  `PROMPT_BUNDLE_VERSION` bumped to `2026.09.11-draft-prompts-3`). DRAFT-015.
- **Project-input evidence snapshots.** Uploaded project inputs now mint
  immutable `[D#]` evidence snapshots at research time (input-id order,
  full parsed text as the passage, sha256 content hash), persisted via the
  existing `draft_evidence.draft_input_id` plumbing. `source_only` continues
  to track vault retrieval only, so a source-only run still requires the
  human acknowledgement while its `[D#]` citations resolve. Manuscript
  passages now reach the outline and per-section drafting prompts through
  the standard evidence registry, so rewrite mode sees content beyond the
  first five facet sentences. DRAFT-016.
- **Code and locked text survive cleanup.** The deterministic lint now
  recognizes CommonMark fences (backtick and tilde, 3+ markers, info strings)
  and multi-backtick inline code spans; bounded rewrites skip anything
  overlapping a code span. User-locked spans are resolved into
  candidate-relative ranges and excluded from initial lint and every
  relint/rewrite, with a post-rewrite preservation verification. Whitespace
  variations of boilerplate (line-wraps, tabs) now rewrite exactly like the
  single-space form. DRAFT-017, DRAFT-018, DRAFT-019.
- **Finding history is fully reachable.** The findings list filters and
  counts in SQL (`status`/`severity` bindings + `COUNT(*)`), and a finding
  is fetched directly by id within its draft, so drafts with more than 2000
  findings keep exact totals, reachable last pages, working filters and
  working direct-ID disposition. The disposition update is now bound to the
  owning draft. DRAFT-021.
- **Export discloses unresolved issues.** The revision export response adds
  an `X-Draft-Open-Blockers` header (and the audit event records
  `open_blockers`) alongside the existing content-sha header that identifies
  the exact checked revision. AC13.

### Frontend — first-class draft review
- Clicking a finding selects its span in the editor and opens the related
  evidence/claims view, with a keyboard path ("Show in editor") and focus
  return. PRODUCT-ENH-08 / LIVE-07.
- Finding categories and rules render with human-readable explanations; the
  raw codes stay available on demand in a collapsed Diagnostics disclosure.
- The export dialog shows an unresolved-blocker disclosure and explains the
  distinction between a completed fact check, per-claim support, the current
  Ready approval, and ready-to-publish.
- Assignment briefs can be saved as named templates (stored locally; the
  brief only — evidence and approvals are never carried) and reapplied to a
  fresh form. Source freshness is unaffected: every compile re-runs research.
- Editing the draft now surfaces a "checks reflect the last saved revision"
  notice on the findings panel, so stale checks are visible rather than
  silently trusted.

### Tests
- Frozen acceptance checks (`backend/tests/draft_room/test_issue517_*.py`,
  `frontend/src/**/*.issue517.test.tsx`) pin each behavior above RED on the
  pre-fix tree and GREEN after.
- Existing research/quality/pipeline/prompts suites updated where the
  evidence contract changed: minted `[D#]` snapshots are now part of the
  job's evidence set (vault `source_only` semantics unchanged and still
  pinned).
- New source-level guardrail `test_issue517_guardrail.py` fails if any lint
  or rewrite call site stops forwarding `locked_spans`.

## Why

Issue #517 (Workstream D, PR 3 of 3): "Drafting preserves input evidence and
locked wording; every editorial/factual finding is actionable against the
exact revision, and verification language never overstates what was checked."
Each owned audit finding (DRAFT-008 through DRAFT-021) was re-anchored on
master, reproduced with a failing check, and repaired minimally.

## Rollback

Single PR, no schema migrations (the `draft_input` evidence family and
`draft_input_id` column already existed). Reverting the PR restores prior
behavior; prompt-bundle version bump invalidates only in-flight checkpointed
desk stages, which re-run cleanly. Exports produced while the header existed
remain valid markdown files.

## Operator-visible outcomes

- Reviewers see Copy/Standards observations and paraphrased unsupported
  claims in the findings list instead of losing them.
- Locked boilerplate, code examples, and uploaded manuscripts survive
  cleanup and rewrite compiles byte-identically.
- Fact status can no longer read "passed" while an adverse claim went
  missing; claim rows carry their retrieval audit.
- Drafts with long finding histories can filter and disposition across the
  full history, not just the newest 2000 rows.
